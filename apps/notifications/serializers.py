"""Lecture des notifications et des alertes."""
from rest_framework import serializers

from apps.notifications.models import Alert, Notification


class NotificationSerializer(serializers.ModelSerializer):
    """
    Ce qu'un membre a reçu.

    ⚠ `user` et `organization` sont en LECTURE SEULE : une notification
    s'adresse à quelqu'un, et laisser le client la réattribuer lui permettrait
    de poser un message dans la boîte d'un collègue.
    """

    class Meta:
        model = Notification
        fields = [
            'id', 'notification_type', 'title', 'message',
            'action_url', 'action_label', 'data',
            'is_read', 'read_at', 'created_at',
        ]
        read_only_fields = fields


class AlertSerializer(serializers.ModelSerializer):
    alert_type_display = serializers.CharField(
        source='get_alert_type_display', read_only=True
    )
    severity_display = serializers.CharField(
        source='get_severity_display', read_only=True
    )
    acknowledged_by_name = serializers.CharField(
        source='acknowledged_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = Alert
        fields = [
            'id', 'alert_type', 'alert_type_display', 'severity',
            'severity_display', 'status', 'title', 'message',
            'resource_type', 'resource_id', 'data',
            'acknowledged_by', 'acknowledged_by_name', 'acknowledged_at',
            'resolved_at', 'created_at',
        ]
        read_only_fields = fields
